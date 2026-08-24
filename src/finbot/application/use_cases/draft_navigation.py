from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from finbot.application.draft_conflicts import PENDING_DRAFT_INTENT_KEY
from finbot.application.draft_navigation import (
    DRAFT_NAVIGATION_ALLOWED_STATES,
    DraftCatalogChoice,
    DraftCatalogRef,
    DraftDateChoice,
    DraftNavigationAccountQuery,
    DraftNavigationAction,
    DraftNavigationCatalogPort,
    DraftNavigationCategoryQuery,
    DraftNavigationChoices,
    DraftNavigationClock,
    DraftNavigationCommand,
    DraftNavigationOwnerQuery,
    DraftNavigationResult,
    DraftNavigationStatus,
)
from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    DraftRef,
    DraftSnapshot,
    OwnerSnapshot,
    UpdateDraftCommand,
)
from finbot.application.errors import (
    ApplicationValidationError,
    CatalogUnavailableError,
    DraftRevisionConflictError,
    InvalidStateError,
)
from finbot.application.rules import learning_candidates
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.domain.money import MoneyError, validate_minor
from finbot.domain.transactions import TransactionType

_REVIEW_RETURN_STATES = frozenset({"wizard_confirm", "quick_confirm", "review"})
_CATEGORY_STATES = frozenset(
    {"wizard_category", "quick_category", "category_required", "review_category"}
)
_ACCOUNT_STATES = frozenset(
    {"wizard_account", "quick_account", "account_required", "review_account"}
)
_SELECTION_ACTIONS = frozenset(
    {
        DraftNavigationAction.SELECT_TYPE,
        DraftNavigationAction.SELECT_CATEGORY,
        DraftNavigationAction.SELECT_ACCOUNT,
        DraftNavigationAction.SELECT_DATE,
    }
)


class SystemDraftNavigationClock:
    """Timezone-aware production clock kept behind an injectable port."""

    def now(self, timezone: str) -> datetime:
        return datetime.now(ZoneInfo(timezone))


def _require_current(
    current: DraftSnapshot | None,
    expected: DraftRef,
) -> DraftSnapshot:
    if current is None or current.ref != expected:
        raise DraftRevisionConflictError(
            current_revision=current.revision if current is not None else None
        )
    if current.suspended:
        raise InvalidStateError("Черновик приостановлен")
    if PENDING_DRAFT_INTENT_KEY in current.payload:
        raise InvalidStateError("Сначала выберите действие для незавершённого ввода")
    return current


def _payload_kind(payload: Mapping[str, Any]) -> TransactionType:
    try:
        return TransactionType(str(payload["type"]))
    except KeyError, ValueError:
        raise ApplicationValidationError("В черновике не указан тип операции") from None


def _required_return_state(payload: dict[str, object], key: str) -> str:
    value = str(payload.pop(key, ""))
    if value not in _REVIEW_RETURN_STATES:
        raise InvalidStateError("Черновик не содержит безопасного экрана возврата")
    return value


def _clear_rule_learning(payload: dict[str, object]) -> None:
    payload.pop("rule_offer_pattern", None)
    payload.pop("pending_rule", None)


def _drop_category(payload: dict[str, object]) -> None:
    for key in ("category_id", "category_name", "category_emoji", "category_slug"):
        payload.pop(key, None)
    _clear_rule_learning(payload)


def _drop_account(payload: dict[str, object], *, drop_date: bool = False) -> None:
    for key in ("account_id", "account_name", "account_slug", "currency"):
        payload.pop(key, None)
    if drop_date:
        payload.pop("occurred_at", None)
    _clear_rule_learning(payload)


def _canonicalize_amount(payload: dict[str, object]) -> None:
    """Normalize rolling-compatible draft payloads to the application money key."""

    amount = payload.get("amount_minor", payload.get("amount"))
    if amount is None:
        return
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise InvalidStateError("Черновик содержит некорректную сумму")
    try:
        validated = validate_minor(amount)
    except MoneyError:
        raise InvalidStateError("Черновик содержит некорректную сумму") from None
    payload["amount_minor"] = validated
    payload.pop("amount", None)


def _is_amount_only_quick(payload: Mapping[str, Any]) -> bool:
    if payload.get("flow") != "quick" or payload.get("input_mode") != "amount_only":
        return False
    if "amount_minor" not in payload:
        raise InvalidStateError("Черновик с быстрым вводом не содержит сумму")
    return True


def _attach_category(payload: dict[str, object], category: CategorySnapshot) -> None:
    payload.update(
        {
            "category_id": str(category.category_id),
            "category_name": category.name,
            "category_emoji": category.emoji,
        }
    )
    payload.pop("category_slug", None)


def _attach_account(payload: dict[str, object], account: AccountSnapshot) -> None:
    if payload.get("account_id") is not None and str(payload["account_id"]) != str(
        account.account_id
    ):
        _clear_rule_learning(payload)
    payload.update(
        {
            "account_id": str(account.account_id),
            "account_name": account.name,
            "currency": account.currency,
        }
    )
    payload.pop("account_slug", None)


def _stage_rule_offer(payload: dict[str, object], previous_category_id: object) -> None:
    """Mirror the proven quick-flow learning offer without persisting a rule."""

    if str(payload.get("flow")) != "quick" or bool(payload.get("category_explicit")):
        _clear_rule_learning(payload)
        return
    if str(previous_category_id) == str(payload.get("category_id")):
        return
    candidates = learning_candidates(str(payload.get("description", "")))
    if candidates:
        payload["rule_offer_pattern"] = candidates[0]
    else:
        payload.pop("rule_offer_pattern", None)
    payload.pop("pending_rule", None)


def _catalog_ref(command: DraftNavigationCommand) -> DraftCatalogRef:
    if not isinstance(command.choice, DraftCatalogRef):  # pragma: no cover - DTO validates this
        raise TypeError("Draft catalog choice is invalid")
    return command.choice


def _validate_account(account: AccountSnapshot, reference: DraftCatalogRef | None = None) -> None:
    if account.archived_at is not None:
        raise CatalogUnavailableError("Счёт больше недоступен")
    if reference is not None and (
        account.account_id != reference.entity_id or account.version != reference.version
    ):
        raise CatalogUnavailableError("Счёт больше недоступен")


def _validate_category(
    category: CategorySnapshot,
    kind: TransactionType,
    reference: DraftCatalogRef | None = None,
) -> None:
    if category.archived_at is not None or category.kind is not kind:
        raise CatalogUnavailableError("Категория больше недоступна")
    if reference is not None and (
        category.category_id != reference.entity_id or category.version != reference.version
    ):
        raise CatalogUnavailableError("Категория больше недоступна")


class DraftNavigationUseCases:
    """Exact-revision draft transitions with no presentation or transaction boundary."""

    __slots__ = ("_accounts", "_catalogs", "_categories", "_clock", "_drafts", "_owners")

    def __init__(
        self,
        drafts: DraftUseCases,
        owners: DraftNavigationOwnerQuery,
        accounts: DraftNavigationAccountQuery,
        categories: DraftNavigationCategoryQuery,
        catalogs: DraftNavigationCatalogPort | None = None,
        clock: DraftNavigationClock | None = None,
    ) -> None:
        self._drafts = drafts
        self._owners = owners
        self._accounts = accounts
        self._categories = categories
        self._catalogs = catalogs
        self._clock = clock or SystemDraftNavigationClock()

    async def execute(self, command: DraftNavigationCommand) -> DraftNavigationResult:
        current = _require_current(
            await self._drafts.get_active(command.owner_id),
            command.expected,
        )
        if current.state not in DRAFT_NAVIGATION_ALLOWED_STATES[command.action]:
            raise InvalidStateError("Этот экран черновика уже неактуален")

        owner = await self._owners(command.owner_id)
        payload = dict(current.payload)
        _canonicalize_amount(payload)
        if command.action in _SELECTION_ACTIONS:
            return await self._select(command, owner, current, payload)
        if command.action is DraftNavigationAction.SKIP_DESCRIPTION:
            return await self._skip_description(command, owner, current, payload)
        if command.action is DraftNavigationAction.BACK:
            return await self._back(command, owner, current, payload)
        return await self._edit(command, owner, current, payload)

    def _required_catalogs(self) -> DraftNavigationCatalogPort:
        if self._catalogs is None:
            raise RuntimeError("Draft navigation catalog port is not configured")
        return self._catalogs

    async def _updated(
        self,
        command: DraftNavigationCommand,
        owner: OwnerSnapshot,
        current: DraftSnapshot,
        state: str,
        payload: dict[str, object],
        choices: DraftNavigationChoices | None = None,
    ) -> DraftNavigationResult:
        updated = await self._drafts.update(
            UpdateDraftCommand(command.owner_id, current.ref, state, payload)
        )
        return DraftNavigationResult(
            action=command.action,
            status=DraftNavigationStatus.UPDATED,
            owner=owner,
            draft=updated,
            choices=choices or DraftNavigationChoices(),
        )

    async def _select(
        self,
        command: DraftNavigationCommand,
        owner: OwnerSnapshot,
        current: DraftSnapshot,
        payload: dict[str, object],
    ) -> DraftNavigationResult:
        if command.action is DraftNavigationAction.SELECT_TYPE:
            return await self._select_type(command, owner, current, payload)
        if command.action is DraftNavigationAction.SELECT_CATEGORY:
            return await self._select_category(command, owner, current, payload)
        if command.action is DraftNavigationAction.SELECT_ACCOUNT:
            return await self._select_account(command, owner, current, payload)
        if command.action is DraftNavigationAction.SELECT_DATE:
            return await self._select_date(command, owner, current, payload)
        raise InvalidStateError("Действие с черновиком не поддерживается")  # pragma: no cover

    async def _select_type(
        self,
        command: DraftNavigationCommand,
        owner: OwnerSnapshot,
        current: DraftSnapshot,
        payload: dict[str, object],
    ) -> DraftNavigationResult:
        choice = command.choice
        if not isinstance(choice, TransactionType):  # pragma: no cover - DTO validates this
            raise TypeError("Draft transaction type choice is invalid")
        payload["type"] = choice.value
        _clear_rule_learning(payload)
        if current.state == "wizard_type":
            _drop_category(payload)
            if _is_amount_only_quick(payload):
                return await self._updated(
                    command,
                    owner,
                    current,
                    "wizard_category",
                    payload,
                    await self._category_choices(command, payload),
                )
            return await self._updated(command, owner, current, "wizard_amount", payload)

        return_state = _required_return_state(payload, "review_return_state")
        category = await self._required_catalogs().resolve_fallback_category(
            command.owner_id,
            choice,
        )
        _validate_category(category, choice)
        _attach_category(payload, category)
        return await self._updated(command, owner, current, return_state, payload)

    async def _select_category(
        self,
        command: DraftNavigationCommand,
        owner: OwnerSnapshot,
        current: DraftSnapshot,
        payload: dict[str, object],
    ) -> DraftNavigationResult:
        if command.choice is DraftCatalogChoice.CUSTOM:
            payload["custom_back_state"] = current.state
            return await self._updated(command, owner, current, "custom_category", payload)

        reference = _catalog_ref(command)
        kind = _payload_kind(payload)
        category = await self._required_catalogs().select_category(
            command.owner_id,
            kind,
            reference,
        )
        _validate_category(category, kind, reference)
        previous_category_id = payload.get("category_id")
        _attach_category(payload, category)

        if current.state == "review_category":
            _stage_rule_offer(payload, previous_category_id)
            return_state = _required_return_state(payload, "review_return_state")
            return await self._updated(command, owner, current, return_state, payload)

        if str(previous_category_id) != str(category.category_id):
            _clear_rule_learning(payload)
        if current.state == "wizard_category":
            return await self._updated(
                command,
                owner,
                current,
                "wizard_account",
                payload,
                await self._account_choices(command.owner_id),
            )

        account = await self._required_catalogs().resolve_account(
            command.owner_id,
            str(payload.get("account_hint", "")).strip() or None,
            owner.default_account_id,
        )
        if account is None:
            _drop_account(payload)
            target = "account_required" if current.state == "category_required" else "quick_account"
            return await self._updated(
                command,
                owner,
                current,
                target,
                payload,
                await self._account_choices(command.owner_id),
            )
        _validate_account(account)
        _attach_account(payload, account)
        target = "review" if current.state == "category_required" else "quick_confirm"
        return await self._updated(command, owner, current, target, payload)

    async def _select_account(
        self,
        command: DraftNavigationCommand,
        owner: OwnerSnapshot,
        current: DraftSnapshot,
        payload: dict[str, object],
    ) -> DraftNavigationResult:
        if command.choice is DraftCatalogChoice.CUSTOM:
            payload["custom_back_state"] = current.state
            return await self._updated(command, owner, current, "custom_account", payload)

        reference = _catalog_ref(command)
        account = await self._required_catalogs().select_account(command.owner_id, reference)
        _validate_account(account, reference)
        _attach_account(payload, account)
        if current.state == "review_account":
            target = _required_return_state(payload, "review_return_state")
        elif current.state == "account_required":
            target = "review"
        elif current.state == "wizard_account":
            target = "wizard_date"
        else:
            target = "quick_confirm"
        return await self._updated(command, owner, current, target, payload)

    async def _select_date(
        self,
        command: DraftNavigationCommand,
        owner: OwnerSnapshot,
        current: DraftSnapshot,
        payload: dict[str, object],
    ) -> DraftNavigationResult:
        choice = command.choice
        if not isinstance(choice, DraftDateChoice):  # pragma: no cover - DTO validates this
            raise TypeError("Draft date choice is invalid")
        if choice is DraftDateChoice.CUSTOM:
            target = "review_date_input" if current.state == "review_date" else "custom_date"
            return await self._updated(command, owner, current, target, payload)

        return_state = (
            _required_return_state(payload, "review_return_state")
            if current.state == "review_date"
            else None
        )
        try:
            timezone = ZoneInfo(owner.timezone)
        except ZoneInfoNotFoundError:
            raise ApplicationValidationError("Часовой пояс владельца не поддерживается") from None
        local_now = self._clock.now(owner.timezone)
        if local_now.utcoffset() is None:
            raise ApplicationValidationError("Текущее время должно содержать часовой пояс")
        local_now = local_now.astimezone(timezone)
        occurred = local_now if choice is DraftDateChoice.TODAY else local_now - timedelta(days=1)
        payload["occurred_at"] = occurred.isoformat()

        if return_state is not None:
            return await self._updated(command, owner, current, return_state, payload)
        payload["return_state"] = "wizard_confirm"
        payload["description_back_state"] = "wizard_date"
        return await self._updated(command, owner, current, "wizard_description", payload)

    async def _account_choices(self, owner_id: UUID) -> DraftNavigationChoices:
        return DraftNavigationChoices(accounts=tuple(await self._accounts(owner_id)))

    async def _edit(
        self,
        command: DraftNavigationCommand,
        owner: OwnerSnapshot,
        current: DraftSnapshot,
        payload: dict[str, object],
    ) -> DraftNavigationResult:
        payload["review_return_state"] = current.state
        choices = DraftNavigationChoices()
        target = {
            DraftNavigationAction.EDIT_TYPE: "review_type",
            DraftNavigationAction.EDIT_AMOUNT: "review_amount",
            DraftNavigationAction.EDIT_CATEGORY: "review_category",
            DraftNavigationAction.EDIT_ACCOUNT: "review_account",
            DraftNavigationAction.EDIT_DATE: "review_date",
        }.get(command.action)
        if command.action is DraftNavigationAction.EDIT_DESCRIPTION:
            payload.pop("review_return_state", None)
            payload["return_state"] = current.state
            payload["description_back_state"] = current.state
            target = "wizard_description"
        elif command.action is DraftNavigationAction.EDIT_CATEGORY:
            kind = _payload_kind(payload)
            choices = DraftNavigationChoices(
                categories=tuple(await self._categories(command.owner_id, kind=kind))
            )
        elif command.action is DraftNavigationAction.EDIT_ACCOUNT:
            choices = DraftNavigationChoices(accounts=tuple(await self._accounts(command.owner_id)))
        if target is None:  # pragma: no cover - enum and dispatch are exhaustive
            raise InvalidStateError("Действие с черновиком не поддерживается")
        updated = await self._drafts.update(
            UpdateDraftCommand(command.owner_id, current.ref, target, payload)
        )
        return DraftNavigationResult(
            action=command.action,
            status=DraftNavigationStatus.UPDATED,
            owner=owner,
            draft=updated,
            choices=choices,
        )

    async def _skip_description(
        self,
        command: DraftNavigationCommand,
        owner: OwnerSnapshot,
        current: DraftSnapshot,
        payload: dict[str, object],
    ) -> DraftNavigationResult:
        target = _required_return_state(payload, "return_state")
        payload.pop("description_back_state", None)
        payload["description"] = ""
        _clear_rule_learning(payload)
        updated = await self._drafts.update(
            UpdateDraftCommand(command.owner_id, current.ref, target, payload)
        )
        return DraftNavigationResult(
            action=command.action,
            status=DraftNavigationStatus.UPDATED,
            owner=owner,
            draft=updated,
        )

    async def _back(
        self,
        command: DraftNavigationCommand,
        owner: OwnerSnapshot,
        current: DraftSnapshot,
        payload: dict[str, object],
    ) -> DraftNavigationResult:
        target, choices = await self._back_target(command, current, payload)
        if target is None:
            await self._drafts.cancel(command.owner_id, current.ref)
            return DraftNavigationResult(
                action=command.action,
                status=DraftNavigationStatus.CLOSED,
                owner=owner,
            )
        updated = await self._drafts.update(
            UpdateDraftCommand(command.owner_id, current.ref, target, payload)
        )
        return DraftNavigationResult(
            action=command.action,
            status=DraftNavigationStatus.UPDATED,
            owner=owner,
            draft=updated,
            choices=choices,
        )

    async def _back_target(
        self,
        command: DraftNavigationCommand,
        current: DraftSnapshot,
        payload: dict[str, object],
    ) -> tuple[str | None, DraftNavigationChoices]:
        state = current.state
        if state.startswith("review_"):
            return _required_return_state(payload, "review_return_state"), DraftNavigationChoices()
        if state == "wizard_amount":
            payload.pop("type", None)
            return "wizard_type", DraftNavigationChoices()
        if state == "wizard_category":
            if _is_amount_only_quick(payload):
                payload.pop("type", None)
                _drop_category(payload)
                return "wizard_type", DraftNavigationChoices()
            payload.pop("amount", None)
            payload.pop("amount_minor", None)
            return "wizard_amount", DraftNavigationChoices()
        if state == "custom_category":
            target = str(payload.pop("custom_back_state", ""))
            if target not in _CATEGORY_STATES:
                raise InvalidStateError("Черновик не содержит безопасного экрана возврата")
            return target, await self._category_choices(command, payload)
        if state in {"wizard_account", "quick_account", "account_required"}:
            target = {
                "wizard_account": "wizard_category",
                "quick_account": "quick_category",
                "account_required": "category_required",
            }[state]
            _drop_category(payload)
            return target, await self._category_choices(command, payload)
        if state == "custom_account":
            target = str(payload.pop("custom_back_state", ""))
            if target not in _ACCOUNT_STATES:
                raise InvalidStateError("Черновик не содержит безопасного экрана возврата")
            return target, DraftNavigationChoices(
                accounts=tuple(await self._accounts(command.owner_id))
            )
        if state == "custom_date":
            return "wizard_date", DraftNavigationChoices()
        if state == "wizard_date":
            _drop_account(payload, drop_date=True)
            return "wizard_account", DraftNavigationChoices(
                accounts=tuple(await self._accounts(command.owner_id))
            )
        if state == "wizard_description":
            return self._description_back(payload), DraftNavigationChoices()
        if state == "wizard_confirm":
            payload["return_state"] = "wizard_confirm"
            payload["description_back_state"] = "wizard_date"
            return "wizard_description", DraftNavigationChoices()
        if state in {"quick_confirm", "review"}:
            _drop_account(payload)
            target = "quick_account" if state == "quick_confirm" else "account_required"
            return target, DraftNavigationChoices(
                accounts=tuple(await self._accounts(command.owner_id))
            )
        if state in {"quick_category", "category_required"}:
            return None, DraftNavigationChoices()
        raise InvalidStateError("Для этого экрана возврат недоступен")

    async def _category_choices(
        self,
        command: DraftNavigationCommand,
        payload: Mapping[str, Any],
    ) -> DraftNavigationChoices:
        kind = _payload_kind(payload)
        return DraftNavigationChoices(
            categories=tuple(await self._categories(command.owner_id, kind=kind))
        )

    @staticmethod
    def _description_back(payload: dict[str, object]) -> str:
        back_state = str(payload.pop("description_back_state", ""))
        payload.pop("return_state", None)
        if back_state in _REVIEW_RETURN_STATES:
            return back_state
        if back_state and back_state != "wizard_date":
            raise InvalidStateError("Черновик не содержит безопасного экрана возврата")
        payload.pop("occurred_at", None)
        return "wizard_date"
