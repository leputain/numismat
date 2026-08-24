from finbot.application.draft_composition import PrepareComposedDraftCommand
from finbot.application.draft_navigation import DraftNavigationCatalogPort
from finbot.application.draft_preparation import DraftPreparationState, PreparedDraftResult
from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    OwnerSnapshot,
    ReviewedTransactionInput,
)
from finbot.application.errors import (
    ApplicationValidationError,
    CatalogUnavailableError,
    EntityNotFoundError,
)
from finbot.application.ports import OwnerReader


def _validate_account(account: AccountSnapshot) -> None:
    if account.archived_at is not None:
        raise CatalogUnavailableError("Счёт больше недоступен")
    if (
        len(account.currency) != 3
        or not account.currency.isascii()
        or not account.currency.isalpha()
        or account.currency != account.currency.upper()
    ):
        raise ApplicationValidationError("Валюта счёта повреждена")
    if not account.name.strip() or len(account.name) > 100:
        raise ApplicationValidationError("Счёт повреждён")


def _validate_category(category: CategorySnapshot, command: PrepareComposedDraftCommand) -> None:
    reference = command.values.category
    if category.archived_at is not None or category.kind is not command.values.kind:
        raise CatalogUnavailableError("Категория больше недоступна")
    if reference is not None and (
        category.category_id != reference.entity_id or category.version != reference.version
    ):
        raise CatalogUnavailableError("Категория больше недоступна")
    if not category.name.strip() or len(category.name) > 100 or len(category.emoji) > 16:
        raise ApplicationValidationError("Категория повреждена")


class PrepareComposedDraft:
    """Resolve versioned/default catalogs and build only a reviewed draft payload."""

    __slots__ = ("_catalogs", "_owners")

    def __init__(self, owners: OwnerReader, catalogs: DraftNavigationCatalogPort) -> None:
        self._owners = owners
        self._catalogs = catalogs

    async def execute(self, command: PrepareComposedDraftCommand) -> PreparedDraftResult:
        owner = await self._owners.get_owner(command.owner_id)
        if owner is None:
            raise EntityNotFoundError("Владелец не найден")
        return await self.prepare_for_owner(owner, command)

    async def prepare_for_owner(
        self,
        owner: OwnerSnapshot,
        command: PrepareComposedDraftCommand,
    ) -> PreparedDraftResult:
        if owner.owner_id != command.owner_id:
            raise ApplicationValidationError("Контекст владельца повреждён")
        values = command.values
        if values.account is None:
            account = await self._catalogs.resolve_account(
                command.owner_id,
                None,
                owner.default_account_id,
            )
            if account is None:
                raise CatalogUnavailableError("Счёт недоступен")
        else:
            account = await self._catalogs.select_account(command.owner_id, values.account)
            if (
                account.account_id != values.account.entity_id
                or account.version != values.account.version
            ):
                raise CatalogUnavailableError("Счёт больше недоступен")
        _validate_account(account)

        category = (
            await self._catalogs.resolve_fallback_category(command.owner_id, values.kind)
            if values.category is None
            else await self._catalogs.select_category(
                command.owner_id,
                values.kind,
                values.category,
            )
        )
        _validate_category(category, command)

        reviewed = ReviewedTransactionInput(
            kind=values.kind,
            amount_minor=values.amount_minor,
            account_id=account.account_id,
            category_id=category.category_id,
            occurred_at=values.occurred_at,
            description=values.description,
        )
        payload = dict(reviewed.to_payload())
        payload.update(
            {
                "flow": "quick",
                "currency": account.currency,
                "account_name": account.name,
                "category_name": category.name,
                "category_emoji": category.emoji,
                "category_explicit": values.category is not None,
                "needs_confirmation": False,
            }
        )
        return PreparedDraftResult(DraftPreparationState.REVIEW, payload)
