from datetime import datetime
from zoneinfo import ZoneInfo

from finbot.application.draft_preparation import (
    AmountOnlyQuickDraft,
    DraftCatalogResolver,
    DraftPreparationClock,
    DraftPreparationState,
    PreparedDraftResult,
    PrepareParsedDraftCommand,
    PrepareQuickDraftCommand,
    QuickDraftParser,
    SignedAmountOnlyQuickDraftError,
)
from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    OwnerSnapshot,
)
from finbot.application.errors import (
    ApplicationValidationError,
    EntityNotFoundError,
)
from finbot.application.ports import OwnerReader
from finbot.application.rules import CategoryRuleReader, resolve_learned_category
from finbot.domain.transactions import TransactionDraft


class SystemDraftPreparationClock:
    """Production clock; tests and batch adapters inject deterministic clocks."""

    def now(self, timezone: str) -> datetime:
        return datetime.now(ZoneInfo(timezone))


def _attach_account(payload: dict[str, object], account: AccountSnapshot) -> None:
    payload.update(
        {
            "account_id": str(account.account_id),
            "account_name": account.name,
            "currency": account.currency,
        }
    )


def _attach_category(payload: dict[str, object], category: CategorySnapshot) -> None:
    payload.update(
        {
            "category_id": str(category.category_id),
            "category_name": category.name,
            "category_emoji": category.emoji,
        }
    )


class PrepareParsedDraft:
    """Resolve a parsed transaction into a channel-neutral review draft."""

    def __init__(
        self,
        owners: OwnerReader,
        catalogs: DraftCatalogResolver,
        category_rules: CategoryRuleReader,
        clock: DraftPreparationClock,
    ) -> None:
        self._owners = owners
        self._catalogs = catalogs
        self._category_rules = category_rules
        self._clock = clock

    async def execute(self, command: PrepareParsedDraftCommand) -> PreparedDraftResult:
        owner = await self._owners.get_owner(command.owner_id)
        if owner is None:
            raise EntityNotFoundError("Владелец не найден")
        return await self.prepare_for_owner(owner, command.draft, flow=command.flow)

    async def prepare_for_owner(
        self,
        owner: OwnerSnapshot,
        draft: TransactionDraft,
        *,
        flow: str,
    ) -> PreparedDraftResult:
        """Shared path for sibling use cases that already loaded the owner."""

        if not flow or len(flow) > 30:
            raise ApplicationValidationError("Некорректный тип черновика")
        occurred_at = draft.occurred_at or self._clock.now(owner.timezone)
        if occurred_at.utcoffset() is None:
            raise ApplicationValidationError("Время операции должно содержать часовой пояс")

        payload: dict[str, object] = {
            "flow": flow,
            "type": draft.type.value,
            "amount_minor": draft.amount_minor,
            "occurred_at": occurred_at.isoformat(),
            "description": draft.description,
            "category_explicit": draft.category_explicit,
            "needs_confirmation": draft.needs_confirmation,
        }
        if draft.account_hint is not None:
            payload["account_hint"] = draft.account_hint
        if draft.category_hint is not None:
            payload["category_hint"] = draft.category_hint

        account = await self._catalogs.resolve_account(
            owner.owner_id,
            draft.account_hint,
            owner.default_account_id,
        )
        if account is not None:
            _attach_account(payload, account)

        category: CategorySnapshot | None = None
        if not draft.category_explicit:
            decision = await resolve_learned_category(
                self._category_rules,
                user_id=owner.owner_id,
                kind=draft.type,
                account_id=account.account_id if account is not None else None,
                description=draft.description,
            )
            if decision is not None:
                category = await self._catalogs.get_category(
                    owner.owner_id,
                    draft.type,
                    decision.category_id,
                )
        if category is None:
            category = await self._catalogs.resolve_category(
                owner.owner_id,
                draft.type,
                draft.category_hint,
            )
        if category is None:
            return PreparedDraftResult(DraftPreparationState.CATEGORY_REQUIRED, payload)
        _attach_category(payload, category)

        if account is None:
            return PreparedDraftResult(DraftPreparationState.ACCOUNT_REQUIRED, payload)
        return PreparedDraftResult(DraftPreparationState.REVIEW, payload)


class PrepareQuickDraft:
    """Parse owner text and delegate all preparation decisions to the shared path."""

    def __init__(
        self,
        owners: OwnerReader,
        parser: QuickDraftParser,
        parsed_drafts: PrepareParsedDraft,
    ) -> None:
        self._owners = owners
        self._parser = parser
        self._parsed_drafts = parsed_drafts

    async def execute(self, command: PrepareQuickDraftCommand) -> PreparedDraftResult:
        owner = await self._owners.get_owner(command.owner_id)
        if owner is None:
            raise EntityNotFoundError("Владелец не найден")
        try:
            draft = self._parser.parse(command.text, timezone=owner.timezone)
        except SignedAmountOnlyQuickDraftError:
            raise ApplicationValidationError("Отправьте сумму без знака, например: 500") from None
        except ValueError:
            raise ApplicationValidationError("Быстрый ввод не распознан") from None
        if isinstance(draft, AmountOnlyQuickDraft):
            return PreparedDraftResult(
                DraftPreparationState.TYPE_REQUIRED,
                {
                    "flow": command.flow,
                    "input_mode": "amount_only",
                    "amount_minor": draft.amount_minor,
                },
            )
        return await self._parsed_drafts.prepare_for_owner(owner, draft, flow=command.flow)
